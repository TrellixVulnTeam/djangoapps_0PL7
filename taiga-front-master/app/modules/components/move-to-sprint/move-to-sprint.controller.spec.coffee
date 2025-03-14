###
# Copyright (C) 2014-2018 Taiga Agile LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
# File: components/move-to-sprint/move-to-sprint-controller.spec.coffee
###

describe "MoveToSprint", ->
    provide = null
    $controller = null
    scope = null
    ctrl = null
    mocks = {}

    _mockTgLightboxFactory = () ->
        mocks.tgLightboxFactory = {
            create: sinon.stub()
        }

        provide.value "tgLightboxFactory", mocks.tgLightboxFactory

    _mockTgProjectService = () ->
        mocks.tgProjectService = {
            project: Immutable.fromJS({
                my_permissions: ['modify_us', 'modify_task', 'modify_issue']
            })
        }

        provide.value "tgProjectService", mocks.tgProjectService

    _mocks = () ->
        module ($provide) ->
            provide = $provide
            _mockTgLightboxFactory()
            _mockTgProjectService()
            return null

    _inject = ->
        inject (_$controller_, $rootScope) ->
            $controller = _$controller_
            scope = $rootScope.$new()

    _setup = ->
        _mocks()
        _inject()

    beforeEach ->
        module "taigaComponents"
        _setup()

        ctrl = $controller("MoveToSprintCtrl", {
            $scope: scope
        }, {
            uss: null,
            unnasignedTasks: null,
            issues: null,
            disabled: false
        })

    describe "button", ->
        it "is disabled by default", () ->
            expect(ctrl.hasOpenItems).to.be.false

        it "is enabled when there are unfinished user stories", () ->
            ctrl.uss = [
                { id: 1, is_closed: true, sprint_order: 5 }
                { id: 2, is_closed: false, sprint_order: 6 }
                { id: 3, is_closed: false, sprint_order: 7 }
            ]
            ctrl.getOpenUss()
            expect(ctrl.hasOpenItems).to.be.true
            expect(ctrl.openItems.uss).to.be.eql([
              { us_id: 2, order: 6 }
              { us_id: 3, order: 7 }
            ])

        it "is enabled when there are unfinished storyless tasks", () ->
            ctrl.unnasignedTasks = [['1', '2'],  ['3']]

            ctrl.taskMap = Immutable.fromJS({
                1: Immutable.fromJS({ model: { id: 1, is_closed: true, taskboard_order: 5 } }),
                2: Immutable.fromJS({ model: { id: 2, is_closed: false, taskboard_order: 6 } }),
                3: Immutable.fromJS({ model: { id: 3, is_closed: false, taskboard_order: 7 } })
            })

            ctrl.getOpenStorylessTasks()
            expect(ctrl.hasOpenItems).to.be.true
            expect(ctrl.openItems.tasks).to.be.eql([
              { task_id: 2, order: 6 }
              { task_id: 3, order: 7 }
            ])

        it "is enabled when there are unfinished issues", () ->
            ctrl.issues = Immutable.fromJS([
              { id: 1, status: { is_closed: true } }
              { id: 2, status: { is_closed: false } }
            ])
            ctrl.getOpenIssues()
            expect(ctrl.hasOpenItems).to.be.true
            expect(ctrl.openItems.issues).to.be.eql([{ issue_id: 2 }])

    describe "lightbox", ->
        it "is opened on button click if there are unfinished items", () ->
            ctrl.issues = Immutable.fromJS([
              { id: 1, status: { is_closed: false } }
            ])
            ctrl.getOpenIssues()
            ctrl.openLightbox()
            expect(mocks.tgLightboxFactory.create).have.been.called

        it "is not opened on button click if there are no unfinished items", () ->
            ctrl.openLightbox()
            expect(mocks.tgLightboxFactory.create).not.have.been.called
